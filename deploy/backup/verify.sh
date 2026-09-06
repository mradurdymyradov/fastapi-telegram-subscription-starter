#!/usr/bin/env bash
# GK-437: a backup that cannot be restored looks exactly like a backup.
#
# GK-427 made backups real — they survive a deploy, they are encrypted, and a
# failed run alerts. It could not answer the only question that matters on the
# day you need one: does this file still turn back into a database? On
# 2026-08-09 the answer was no. The age private key on file failed its own
# checksum, every dump taken until then was permanently unreadable, and the
# backup job kept reporting success the whole time.
#
# So the claim worth making is not "the backup job succeeded" but "the newest
# backup was restorable as of X". This script earns that sentence: decrypt the
# newest encrypted dump, restore it into a throwaway database, compare it table
# by table against live, and write the verdict into `backup_verifications`
# where the admin panel and the bot's staleness check can both see it.
#
# Required env (beyond the POSTGRES_* the backup image already needs):
#   BACKUP_AGE_IDENTITY_FILE — the age PRIVATE key. This script is the only
#                              thing that ever reads it before an emergency,
#                              which is the entire point. It is mounted into
#                              THIS service only; the backup container that
#                              writes the dumps never holds it.
# Optional:
#   BACKUP_DIR                      default /var/backups/membership_saas
#   BACKUP_VERIFY_DB                default membership_saas_restore_check
#   BACKUP_VERIFY_MAX_AGE_HOURS     default 30 — a dump older than this is a
#                                   backup failure even if it restores fine

set -euo pipefail

: "${POSTGRES_HOST:?POSTGRES_HOST required}"
: "${POSTGRES_USER:?POSTGRES_USER required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}"
: "${POSTGRES_DB:?POSTGRES_DB required}"
: "${BACKUP_AGE_IDENTITY_FILE:?BACKUP_AGE_IDENTITY_FILE required (path to the age PRIVATE key)}"

BACKUP_DIR="${BACKUP_DIR:-/var/backups/membership_saas}"
VERIFY_DB="${BACKUP_VERIFY_DB:-membership_saas_restore_check}"
MAX_AGE_HOURS="${BACKUP_VERIFY_MAX_AGE_HOURS:-30}"

# Belt and braces: the restore target must never be the live database. The
# script drops it first, so a typo here would not be recoverable.
if [ "$VERIFY_DB" = "$POSTGRES_DB" ]; then
    echo "[verify] REFUSING: BACKUP_VERIFY_DB equals POSTGRES_DB ($POSTGRES_DB)" >&2
    exit 2
fi

log() { printf '[verify] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

WORK_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$WORK_DIR"
    # Drop the scratch database whatever happened, including on the failure
    # paths — a half-restored copy of production left lying around is its own
    # problem. `|| true`: this runs from a trap and must not mask the real exit.
    PGPASSWORD="$POSTGRES_PASSWORD" psql --host="$POSTGRES_HOST" --username="$POSTGRES_USER" \
        --dbname=postgres --quiet -c "DROP DATABASE IF EXISTS \"$VERIFY_DB\" WITH (FORCE)" \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT

psql_at() {
    local db="$1"
    shift
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        --host="$POSTGRES_HOST" --username="$POSTGRES_USER" --dbname="$db" \
        --no-align --tuples-only --quiet --set ON_ERROR_STOP=on "$@"
}

# `record <ok> <tables> <mismatches> <detail> [dump]` — the verdict has to land
# in the database even when the verification failed; a failure nobody can see is
# the thing this script exists to prevent. psql's `:'var'` does the quoting, so
# a detail string containing quotes cannot break the statement.
record() {
    local ok="$1" tables="$2" mismatches="$3" detail="$4" dump="${5:-}"
    PGPASSWORD="$POSTGRES_PASSWORD" psql --host="$POSTGRES_HOST" --username="$POSTGRES_USER" \
        --dbname="$POSTGRES_DB" --quiet --set ON_ERROR_STOP=on \
        --set "v_ok=$ok" --set "v_tables=$tables" --set "v_mis=$mismatches" \
        --set "v_detail=$detail" --set "v_dump=$dump" \
        <<'SQL' >/dev/null || log "WARNING: could not record the verification result"
INSERT INTO backup_verifications (verified_at, dump_file, ok, tables_checked, mismatches, detail)
VALUES (now(), NULLIF(:'v_dump', ''), :v_ok, :v_tables, :v_mis, :'v_detail');
SQL
}

fail() {
    local detail="$1" dump="${2:-}"
    log "FAILED: $detail"
    record false 0 0 "$detail" "$dump"
    exit 1
}

# ---------------------------------------------------------------------------
# 1. is there a recent backup at all?
# ---------------------------------------------------------------------------

# `ls -t` and not `find -printf`: the image is Alpine, whose busybox `find` has
# no `-printf`. GNU coreutils supplies `ls`/`stat`, which is what we have.
newest="$(ls -1t "$BACKUP_DIR"/membership_saas-*.sql.gz.age 2>/dev/null | head -1)"

if [ -z "$newest" ]; then
    fail "no encrypted dump found in $BACKUP_DIR — there is nothing to restore"
fi

age_seconds=$(( $(date -u +%s) - $(stat -c %Y "$newest") ))
age_hours=$(( age_seconds / 3600 ))
log "newest dump: $(basename "$newest") ($age_hours h old, $(stat -c%s "$newest") bytes)"

if [ "$age_hours" -gt "$MAX_AGE_HOURS" ]; then
    fail "newest dump is ${age_hours}h old (limit ${MAX_AGE_HOURS}h) — backups have stopped" \
        "$(basename "$newest")"
fi

# ---------------------------------------------------------------------------
# 2. does the private key still open it?
# ---------------------------------------------------------------------------
#
# This step alone would have caught the 2026-08-09 corruption on the first
# night instead of at the restore that mattered.

plain="$WORK_DIR/dump.sql.gz"
if ! age --decrypt --identity "$BACKUP_AGE_IDENTITY_FILE" --output "$plain" "$newest" 2>"$WORK_DIR/age.err"; then
    fail "DECRYPTION FAILED with the configured identity: $(head -c 300 "$WORK_DIR/age.err")" \
        "$(basename "$newest")"
fi
if ! gzip -t "$plain" 2>"$WORK_DIR/gzip.err"; then
    fail "decrypted file is not a valid gzip stream: $(head -c 300 "$WORK_DIR/gzip.err")" \
        "$(basename "$newest")"
fi
log "decrypted and gzip-verified"

# ---------------------------------------------------------------------------
# 3. does it restore?
# ---------------------------------------------------------------------------

psql_at postgres -c "DROP DATABASE IF EXISTS \"$VERIFY_DB\" WITH (FORCE)" >/dev/null
psql_at postgres -c "CREATE DATABASE \"$VERIFY_DB\"" >/dev/null
log "restoring into scratch database $VERIFY_DB"

if ! gunzip -c "$plain" | psql_at "$VERIFY_DB" >"$WORK_DIR/restore.log" 2>&1; then
    fail "restore into $VERIFY_DB failed: $(tail -c 400 "$WORK_DIR/restore.log")" \
        "$(basename "$newest")"
fi

# ---------------------------------------------------------------------------
# 4. is what came back actually the database?
# ---------------------------------------------------------------------------
#
# The dump is a point in time and live has moved on since, so equal counts are
# not the test — a table that exists in live and is EMPTY or ABSENT in the
# restore is. Anything else is reported as drift and is expected.
#
# One known and accepted false positive: a table that gains its first-ever rows
# between two dumps reads as EMPTY once. It clears itself on the next dump, and
# the message names the table, so it is informative rather than mysterious. The
# alternative — relaxing the check — would let a dump that restores structurally
# with no data in it pass, which is the failure this whole script exists for.

# `backup_verifications` is excluded, and the reason is worth writing down: it
# is the table THIS SCRIPT writes. Every run appends a row to live and none to
# the restore, so including it means the canary fails itself on the next run
# for the crime of having run — a self-poisoning check. Caught by the negative
# pass, which is the only reason it is not in production.
count_sql="$(psql_at "$POSTGRES_DB" -c \
    "SELECT string_agg(format('SELECT %L::text AS t, count(*)::bigint AS n FROM public.%I', tablename, tablename), ' UNION ALL ')
     FROM pg_tables WHERE schemaname='public' AND tablename <> 'backup_verifications'")"

if [ -z "$count_sql" ]; then
    fail "live database reports no public tables — refusing to call this a pass" \
        "$(basename "$newest")"
fi

live_counts="$(psql_at "$POSTGRES_DB" -c "$count_sql" | sort)"
# A table missing from the restore makes this query error out, which is exactly
# the verdict we want; ON_ERROR_STOP turns it into a non-zero exit.
if ! restored_counts="$(psql_at "$VERIFY_DB" -c "$count_sql" 2>"$WORK_DIR/counts.err" | sort)"; then
    fail "restored database is missing tables that live has: $(head -c 300 "$WORK_DIR/counts.err")" \
        "$(basename "$newest")"
fi

tables=0
empty=0
drifted=0
detail=""
while IFS='|' read -r table n_live; do
    [ -n "$table" ] || continue
    tables=$(( tables + 1 ))
    n_restored="$(printf '%s\n' "$restored_counts" | awk -F'|' -v t="$table" '$1==t {print $2}')"
    n_restored="${n_restored:-0}"
    if [ "$n_live" -gt 0 ] && [ "$n_restored" -eq 0 ]; then
        empty=$(( empty + 1 ))
        detail="${detail}${table}: live=$n_live restored=0 (EMPTY); "
    elif [ "$n_restored" != "$n_live" ]; then
        drifted=$(( drifted + 1 ))
        detail="${detail}${table}: live=$n_live restored=$n_restored; "
    fi
done <<< "$live_counts"

log "compared $tables tables: $empty empty in the restore, $drifted with counts moved on since the dump"

if [ "$empty" -gt 0 ]; then
    fail "restore is missing data in $empty table(s): $detail" "$(basename "$newest")"
fi

summary="restored $(basename "$newest") (${age_hours}h old); $tables tables; $drifted with counts moved on since the dump"
[ -n "$detail" ] && summary="$summary — $detail"
record true "$tables" "$drifted" "$summary" "$(basename "$newest")"
log "OK: $summary"
