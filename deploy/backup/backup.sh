#!/usr/bin/env bash
# GK-040: nightly PostgreSQL backup → age-encrypted → object storage.
#
# Required env:
#   POSTGRES_HOST, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
#   BACKUP_AGE_PUBLIC_KEY        — recipient pubkey, e.g. "age1xyz..."
#   BACKUP_RCLONE_REMOTE         — rclone remote:bucket/prefix (S3/R2/B2)
# Optional env:
#   BACKUP_RETENTION_DAYS        — default 14. LOCAL copies only.
#   BACKUP_REMOTE_RETENTION_DAYS — unset = never prune the remote (correct for
#                                  S3/R2, where a bucket lifecycle policy does
#                                  it). Set it for targets with no lifecycle
#                                  support, e.g. Google Drive. See the block
#                                  at the bottom of this file.
#   BACKUP_DIR                   — default /var/backups/membership_saas
#   BACKUP_ALERT_WEBHOOK_URL     — POST {text} on failure (e.g. healthchecks.io)
#
# Fails loud: any non-zero step exits 1 and the entrypoint loop posts an
# alert. Local copies are kept BACKUP_RETENTION_DAYS days so a quick
# rollback does not need to round-trip object storage.
#
# Exit codes: 0 ok · 1 the backup failed · 3 the backup succeeded but remote
# retention did not run.

set -euo pipefail

: "${POSTGRES_HOST:?POSTGRES_HOST required}"
: "${POSTGRES_USER:?POSTGRES_USER required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}"
: "${POSTGRES_DB:?POSTGRES_DB required}"
: "${BACKUP_AGE_PUBLIC_KEY:?BACKUP_AGE_PUBLIC_KEY required (age recipient pubkey)}"
: "${BACKUP_RCLONE_REMOTE:?BACKUP_RCLONE_REMOTE required (e.g. s3:membership_saas-backups/prod)}"

BACKUP_DIR="${BACKUP_DIR:-/var/backups/membership_saas}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
dump_file="$BACKUP_DIR/membership_saas-$ts.sql.gz"
enc_file="$dump_file.age"

log() { printf '[backup] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# GK-427: give rclone a WRITABLE copy of the config.
#
# OAuth backends (Google Drive) refresh their short-lived access token on every
# run and then try to persist it back into the config file. That file is
# mounted read-only on purpose — it holds the credential — so the write fails
# and rclone logs
#
#   ERROR : Failed to save config after 10 tries: … permission denied
#
# on every otherwise-successful cycle, after ten retries. The backup still
# works, which is precisely the problem: a system whose entire purpose is that
# failures get noticed cannot afford a routine ERROR line in its happy path.
# People stop reading logs that cry wolf.
#
# A throwaway copy fixes it without weakening anything. The durable half of the
# credential is the refresh token in the mounted file, which does not change;
# the access token rclone wants to save is worth an hour. The real config stays
# read-only, and the copy dies with the process.
#
# Harmless on S3/R2/B2 — static keys are never rewritten, so nothing happens.
conf_src="${RCLONE_CONFIG:-${HOME:-/home/backup}/.config/rclone/rclone.conf}"
if [ -r "$conf_src" ] && [ ! -w "$conf_src" ]; then
    conf_copy="$(mktemp)"
    trap 'rm -f "$conf_copy"' EXIT INT TERM
    cat "$conf_src" > "$conf_copy"
    chmod 600 "$conf_copy"
    export RCLONE_CONFIG="$conf_copy"
fi

log "starting dump host=$POSTGRES_HOST db=$POSTGRES_DB target=$BACKUP_RCLONE_REMOTE"

# pg_dump → gzip in one pipeline. `--no-owner --no-privileges` keeps the
# dump portable across hosts (different role names on restore boxes).
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
    --host="$POSTGRES_HOST" \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --format=plain \
    --no-owner \
    --no-privileges \
    --no-acl \
    | gzip -9 > "$dump_file"

log "dump size $(stat -c%s "$dump_file") bytes — encrypting"

# age never touches the symmetric key; only the recipient pubkey is in env.
# Private key stays in the operator's vault — see runbook.
age --recipient "$BACKUP_AGE_PUBLIC_KEY" --output "$enc_file" "$dump_file"
rm -f "$dump_file"

log "uploading $(basename "$enc_file") → $BACKUP_RCLONE_REMOTE"
rclone copy "$enc_file" "$BACKUP_RCLONE_REMOTE" --s3-no-check-bucket --quiet

# Local retention: drop encrypted dumps older than RETENTION_DAYS.
find "$BACKUP_DIR" -name 'membership_saas-*.sql.gz.age' -type f -mtime +"$RETENTION_DAYS" -print -delete \
    | while read -r old; do log "pruned local $old"; done

log "ok ts=$ts size=$(stat -c%s "$enc_file") remote=$BACKUP_RCLONE_REMOTE"

# ---------------------------------------------------------------------------
# Remote retention (GK-427).
#
# Off unless BACKUP_REMOTE_RETENTION_DAYS is set. On S3/R2 the right tool is a
# bucket lifecycle policy — storage-side, no script involved, no way for a bug
# here to destroy the off-host copies. That was the original reasoning and it
# still holds.
#
# It stopped being sufficient when the target became Google Drive, which has
# no lifecycle policies at all: with nothing here, remote copies grow until
# the quota fills and then the backup itself starts failing. So this exists,
# and it is written to fail SAFE rather than fail open:
#
#   - deletion is by age only, via rclone's own --min-age; we never compute a
#     list of victims here and hand it to a delete;
#   - --include restricts it to our own dump filenames;
#   - if the listing is empty or errored we skip entirely. An unreadable
#     remote means "we don't know what's out there", never "there's nothing
#     worth keeping";
#   - every deletion is logged;
#   - a prune problem exits 3, which the entrypoint reports as its own kind of
#     alert. It must not masquerade as "backup failed" — the backup at that
#     point has already succeeded — and it must not be silent either.
# ---------------------------------------------------------------------------
prune_rc=0
if [ -n "${BACKUP_REMOTE_RETENTION_DAYS:-}" ]; then
    remote_days="$BACKUP_REMOTE_RETENTION_DAYS"
    log "remote retention: pruning $BACKUP_RCLONE_REMOTE older than ${remote_days}d"

    if listing="$(rclone lsf "$BACKUP_RCLONE_REMOTE" \
                    --include 'membership_saas-*.sql.gz.age' 2>&1)" \
       && [ -n "$listing" ]; then
        log "remote retention: $(printf '%s\n' "$listing" | grep -c . || true) dump(s) visible before prune"

        if prune_out="$(rclone delete "$BACKUP_RCLONE_REMOTE" \
                          --include 'membership_saas-*.sql.gz.age' \
                          --min-age "${remote_days}d" \
                          --verbose 2>&1)"; then
            # `|| true` on every grep in this block: with `set -o pipefail` a
            # grep that matches nothing (the normal case — most nights delete
            # zero objects) fails the pipeline and would abort the script.
            printf '%s\n' "$prune_out" | { grep -i 'Deleted' || true; } | while read -r line; do
                log "remote-pruned $line"
            done
            log "remote retention: $(printf '%s\n' "$prune_out" | grep -ci 'Deleted' || true) object(s) deleted, $(rclone lsf "$BACKUP_RCLONE_REMOTE" --include 'membership_saas-*.sql.gz.age' 2>/dev/null | grep -c . || true) remaining"
        else
            prune_rc=3
            log "remote retention FAILED: rclone delete returned non-zero — nothing was pruned"
            # head -5: a usage error makes rclone print its entire help text,
            # which buries the one line that says what actually went wrong.
            printf '%s\n' "$prune_out" | head -5 | while read -r line; do log "remote-prune! $line"; done
        fi
    else
        prune_rc=3
        log "remote retention SKIPPED: listing empty or failed — refusing to prune blind"
        printf '%s\n' "$listing" | head -5 | while read -r line; do log "remote-prune! $line"; done
    fi
fi

# Optional: hit a healthchecks.io-style URL so external monitoring knows we
# completed (and pages if we don't).
if [ -n "${BACKUP_HEALTHCHECK_URL:-}" ]; then
    curl -fsS --max-time 10 "$BACKUP_HEALTHCHECK_URL" >/dev/null || true
fi

# 0 = clean. 3 = the dump was taken, encrypted and uploaded, but remote
# retention could not run. The distinction matters: exit 3 is not a data-loss
# event, it is a "this will become one if ignored" event, and the entrypoint
# words its alert accordingly.
exit "$prune_rc"
