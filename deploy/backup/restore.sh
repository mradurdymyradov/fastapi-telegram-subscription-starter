#!/usr/bin/env bash
# GK-040: restore a single dump into PostgreSQL.
#
# Usage:
#   restore.sh <remote-object-path>   # pulls from $BACKUP_RCLONE_REMOTE
#   restore.sh --local <file.sql.gz.age>
#
# Required env (in addition to BACKUP_RCLONE_REMOTE, POSTGRES_*):
#   BACKUP_AGE_IDENTITY_FILE — path inside the container holding the age
#                              private key. Mount it read-only via compose.
#
# Refuses to restore unless `RESTORE_CONFIRM=YES` is set, so muscle memory
# alone cannot wipe production.

set -euo pipefail

: "${POSTGRES_HOST:?POSTGRES_HOST required}"
: "${POSTGRES_USER:?POSTGRES_USER required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}"
: "${POSTGRES_DB:?POSTGRES_DB required}"
: "${BACKUP_AGE_IDENTITY_FILE:?BACKUP_AGE_IDENTITY_FILE required (path to age private key)}"

if [ "${RESTORE_CONFIRM:-}" != "YES" ]; then
    echo "Refusing to restore: set RESTORE_CONFIRM=YES to proceed." >&2
    exit 2
fi

mode="$1"
shift || true

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

case "$mode" in
    --local)
        enc_file="$1"
        ;;
    *)
        : "${BACKUP_RCLONE_REMOTE:?BACKUP_RCLONE_REMOTE required for remote restore}"
        rclone copy "$BACKUP_RCLONE_REMOTE/$mode" "$WORK_DIR" --s3-no-check-bucket
        enc_file="$WORK_DIR/$(basename "$mode")"
        ;;
esac

log() { printf '[restore] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
log "decrypting $enc_file"
plain="$WORK_DIR/dump.sql.gz"
age --decrypt --identity "$BACKUP_AGE_IDENTITY_FILE" --output "$plain" "$enc_file"

log "streaming psql into $POSTGRES_HOST/$POSTGRES_DB"
PGPASSWORD="$POSTGRES_PASSWORD" psql \
    --host="$POSTGRES_HOST" \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --set ON_ERROR_STOP=on \
    --quiet < <(gunzip -c "$plain")

log "restore complete"
